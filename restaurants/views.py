import math
from django.db.models import Q, Count, F
from django.utils import timezone
from django.core.cache import cache
from rest_framework import generics, viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend

from .filters import RestaurantFilter, DealFilter
from .models import (
    Country, City, RestaurantCategory, Restaurant, Deal,
    SavedRestaurant, SavedDeal, DealUse
)
from .serializers import (
    CountrySerializer, CitySerializer, RestaurantCategorySerializer,
    RestaurantSerializer, RestaurantListSerializer, DealSerializer,
    DealListSerializer, SavedRestaurantSerializer, SavedDealSerializer,
    DealUseSerializer, DealUseCreateSerializer
)
from users.permissions import IsMerchant


def calculate_distance(lat1, lon1, lat2, lon2):
    """Calculate distance between two points using Haversine formula (in km)"""
    if not all([lat1, lon1, lat2, lon2]):
        return None
    R = 6371  # Earth's radius in kilometers
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.asin(math.sqrt(a))
    return R * c


class CountryListView(generics.ListAPIView):
    """List all countries"""
    queryset = Country.objects.all()
    serializer_class = CountrySerializer
    permission_classes = [AllowAny]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "code"]
    ordering_fields = ["name"]
    ordering = ["name"]


class CityListView(generics.ListAPIView):
    """List all cities, optionally filtered by country"""
    serializer_class = CitySerializer
    permission_classes = [AllowAny]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["country", "is_active"]
    search_fields = ["name", "country__name"]
    ordering_fields = ["name", "created_at"]
    ordering = ["name"]
    
    def get_queryset(self):
        return City.objects.filter(is_active=True).select_related("country").annotate(
            restaurants_count=Count(
                "restaurants",
                filter=Q(restaurants__is_active=True, restaurants__verified=True),
                distinct=True
            )
        )


class RestaurantCategoryListView(generics.ListAPIView):
    """List all restaurant categories"""
    queryset = RestaurantCategory.objects.all()
    serializer_class = RestaurantCategorySerializer
    permission_classes = [AllowAny]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name"]
    ordering_fields = ["name"]
    ordering = ["name"]


class RestaurantViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for restaurants"""
    permission_classes = [AllowAny]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = RestaurantFilter
    search_fields = ["name", "description", "address", "city__name"]
    ordering_fields = ["name", "created_at", "is_featured"]
    ordering = ["-is_featured", "-created_at"]
    
    def get_serializer_class(self):
        if self.action == "list":
            return RestaurantListSerializer
        return RestaurantSerializer
    
    def get_queryset(self):
        queryset = Restaurant.objects.filter(
            is_active=True,
            verified=True
        ).select_related("city", "city__country").prefetch_related(
            "categories", "images"
        ).annotate(
            active_deals_count=Count(
                "deals",
                filter=Q(
                    deals__is_active=True,
                    deals__start_date__lte=timezone.now(),
                    deals__end_date__gte=timezone.now()
                ),
                distinct=True
            )
        )
        
        # Filter by category slug if provided
        category_slug = self.request.query_params.get("category")
        if category_slug:
            queryset = queryset.filter(categories__slug=category_slug)
        
        # Nearby restaurants (requires lat/long)
        lat = self.request.query_params.get("latitude")
        lon = self.request.query_params.get("longitude")
        radius = self.request.query_params.get("radius", 10)  # default 10km
        
        if lat and lon:
            try:
                lat = float(lat)
                lon = float(lon)
                radius = float(radius)
                
                # Filter restaurants within approximate radius
                # Simple bounding box filter (not perfect but fast)
                lat_delta = radius / 111.0  # roughly 1 degree = 111km
                lon_delta = radius / (111.0 * abs(math.cos(math.radians(lat))))
                
                queryset = queryset.filter(
                    latitude__gte=lat - lat_delta,
                    latitude__lte=lat + lat_delta,
                    longitude__gte=lon - lon_delta,
                    longitude__lte=lon + lon_delta,
                    latitude__isnull=False,
                    longitude__isnull=False
                )
            except (ValueError, TypeError):
                pass  # Invalid coordinates, ignore filter
        
        return queryset
    
    @action(detail=True, methods=["post", "delete"], permission_classes=[IsAuthenticated])
    def save(self, request, pk=None):
        """Save or unsave a restaurant"""
        restaurant = self.get_object()
        saved, created = SavedRestaurant.objects.get_or_create(
            user=request.user,
            restaurant=restaurant
        )
        
        if request.method == "DELETE":
            saved.delete()
            return Response({"detail": "Restaurant unsaved"}, status=status.HTTP_204_NO_CONTENT)
        
        serializer = SavedRestaurantSerializer(saved, context={"request": request})
        return Response(serializer.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
    
    @action(detail=False, methods=["get"], permission_classes=[IsAuthenticated])
    def saved(self, request):
        """Get user's saved restaurants"""
        saved_restaurants = SavedRestaurant.objects.filter(
            user=request.user
        ).select_related("restaurant", "restaurant__city").prefetch_related(
            "restaurant__images"
        ).order_by("-created_at")
        
        restaurants = [sr.restaurant for sr in saved_restaurants]
        serializer = RestaurantListSerializer(restaurants, many=True, context={"request": request})
        return Response(serializer.data)
    
    @action(detail=False, methods=["get"], permission_classes=[AllowAny])
    def nearby(self, request):
        """Get nearby restaurants based on coordinates"""
        lat = request.query_params.get("latitude")
        lon = request.query_params.get("longitude")
        radius = float(request.query_params.get("radius", 10))
        
        if not lat or not lon:
            return Response(
                {"error": "latitude and longitude are required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            lat = float(lat)
            lon = float(lon)
        except (ValueError, TypeError):
            return Response(
                {"error": "Invalid latitude or longitude"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get all restaurants within bounding box
        lat_delta = radius / 111.0
        lon_delta = radius / (111.0 * abs(math.cos(math.radians(lat))))
        
        restaurants = Restaurant.objects.filter(
            is_active=True,
            verified=True,
            latitude__gte=lat - lat_delta,
            latitude__lte=lat + lat_delta,
            longitude__gte=lon - lon_delta,
            longitude__lte=lon + lon_delta,
            latitude__isnull=False,
            longitude__isnull=False
        ).select_related("city", "city__country").prefetch_related("images")
        
        # Calculate actual distances and sort
        restaurants_with_distance = []
        for restaurant in restaurants:
            if restaurant.latitude and restaurant.longitude:
                distance = calculate_distance(
                    lat, lon,
                    float(restaurant.latitude),
                    float(restaurant.longitude)
                )
                if distance and distance <= radius:
                    restaurants_with_distance.append((restaurant, distance))
        
        # Sort by distance
        restaurants_with_distance.sort(key=lambda x: x[1])
        restaurants = [r[0] for r in restaurants_with_distance]
        
        serializer = RestaurantListSerializer(restaurants, many=True, context={"request": request})
        return Response(serializer.data)


class DealViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for deals"""
    permission_classes = [AllowAny]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = DealFilter
    search_fields = ["title", "description", "restaurant__name"]
    ordering_fields = ["start_date", "end_date", "created_at", "is_featured"]
    ordering = ["-is_featured", "-created_at"]
    
    def get_serializer_class(self):
        if self.action == "list":
            return DealListSerializer
        return DealSerializer
    
    def get_queryset(self):
        now = timezone.now()
        queryset = Deal.objects.filter(
            is_active=True,
            restaurant__is_active=True,
            restaurant__verified=True
        ).select_related("restaurant", "restaurant__city", "restaurant__city__country").prefetch_related(
            "images"
        ).filter(
            start_date__lte=now,
            end_date__gte=now
        )
        
        # Filter by city
        city = self.request.query_params.get("city")
        if city:
            queryset = queryset.filter(restaurant__city__slug=city)
        
        # Filter by country
        country = self.request.query_params.get("country")
        if country:
            queryset = queryset.filter(restaurant__city__country__code=country)
        
        return queryset
    
    @action(detail=True, methods=["post", "delete"], permission_classes=[IsAuthenticated])
    def save(self, request, pk=None):
        """Save or unsave a deal"""
        deal = self.get_object()
        saved, created = SavedDeal.objects.get_or_create(
            user=request.user,
            deal=deal
        )
        
        if request.method == "DELETE":
            saved.delete()
            return Response({"detail": "Deal unsaved"}, status=status.HTTP_204_NO_CONTENT)
        
        serializer = SavedDealSerializer(saved, context={"request": request})
        return Response(serializer.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
    
    @action(detail=False, methods=["get"], permission_classes=[IsAuthenticated])
    def saved(self, request):
        """Get user's saved deals"""
        saved_deals = SavedDeal.objects.filter(
            user=request.user
        ).select_related("deal", "deal__restaurant").prefetch_related(
            "deal__images"
        ).order_by("-created_at")
        
        deals = [sd.deal for sd in saved_deals]
        serializer = DealListSerializer(deals, many=True, context={"request": request})
        return Response(serializer.data)
    
    @action(detail=False, methods=["get"], permission_classes=[AllowAny])
    def active(self, request):
        """Get all active deals (cached)"""
        now = timezone.now()
        cache_key = f"active_deals_{now.date()}"
        deals = cache.get(cache_key)
        
        if deals is None:
            deals = Deal.objects.filter(
                is_active=True,
                restaurant__is_active=True,
                restaurant__verified=True,
                start_date__lte=now,
                end_date__gte=now
            ).select_related(
                "restaurant", "restaurant__city"
            ).prefetch_related("images").order_by("-is_featured", "-created_at")
            cache.set(cache_key, list(deals), 300)  # Cache for 5 minutes
        
        serializer = DealListSerializer(deals, many=True, context={"request": request})
        return Response(serializer.data)
    
    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated])
    def use(self, request, pk=None):
        """Mark a deal as used by the current user"""
        deal = self.get_object()
        
        if not deal.is_active_now():
            return Response(
                {"error": "This deal is not currently active"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not deal.can_user_use(request.user):
            return Response(
                {"error": "You have reached the maximum uses for this deal"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        serializer = DealUseCreateSerializer(
            data={"deal": deal.id, "notes": request.data.get("notes", "")},
            context={"request": request}
        )
        
        if serializer.is_valid():
            deal_use = serializer.save()
            return Response(
                DealUseSerializer(deal_use, context={"request": request}).data,
                status=status.HTTP_201_CREATED
            )
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class DealUseViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for user's deal uses"""
    serializer_class = DealUseSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["deal", "restaurant_confirmed"]
    ordering_fields = ["used_at", "created_at"]
    ordering = ["-used_at"]
    
    def get_queryset(self):
        return DealUse.objects.filter(user=self.request.user).select_related(
            "deal", "deal__restaurant"
        )


class MerchantRestaurantViewSet(viewsets.ModelViewSet):
    """ViewSet for merchants to manage their restaurants"""
    serializer_class = RestaurantSerializer
    permission_classes = [IsMerchant]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "address"]
    ordering_fields = ["name", "created_at"]
    ordering = ["-created_at"]
    
    def get_queryset(self):
        # Get merchant's restaurants
        try:
            merchant = self.request.user.merchant
        except (AttributeError, Exception) as e:
            # RelatedObjectDoesNotExist or any other exception
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Merchant profile not found. Please create a merchant account.")
        return Restaurant.objects.filter(
            merchant=merchant
        ).select_related("city", "city__country").prefetch_related(
            "categories", "images"
        ).annotate(
            active_deals_count=Count(
                "deals",
                filter=Q(
                    deals__is_active=True,
                    deals__start_date__lte=timezone.now(),
                    deals__end_date__gte=timezone.now()
                ),
                distinct=True
            )
        )
    
    def perform_create(self, serializer):
        try:
            merchant = self.request.user.merchant
        except (AttributeError, Exception) as e:
            # RelatedObjectDoesNotExist or any other exception
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Merchant profile not found. Please create a merchant account.")
        serializer.save(merchant=merchant)


class MerchantDealViewSet(viewsets.ModelViewSet):
    """ViewSet for merchants to manage their deals"""
    serializer_class = DealSerializer
    permission_classes = [IsMerchant]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["restaurant", "deal_type", "is_featured"]
    search_fields = ["title", "description"]
    ordering_fields = ["start_date", "end_date", "created_at"]
    ordering = ["-created_at"]
    
    def get_queryset(self):
        # Get deals for merchant's restaurants
        try:
            merchant = self.request.user.merchant
        except (AttributeError, Exception) as e:
            # RelatedObjectDoesNotExist or any other exception
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Merchant profile not found. Please create a merchant account.")
        return Deal.objects.filter(
            restaurant__merchant=merchant
        ).select_related("restaurant").prefetch_related("images")
    
    def perform_create(self, serializer):
        restaurant_id = self.request.data.get("restaurant")
        try:
            merchant = self.request.user.merchant
        except (AttributeError, Exception) as e:
            # RelatedObjectDoesNotExist or any other exception
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Merchant profile not found. Please create a merchant account.")
        
        # Verify restaurant belongs to merchant
        try:
            restaurant = Restaurant.objects.get(id=restaurant_id, merchant=merchant)
        except Restaurant.DoesNotExist:
            from rest_framework.exceptions import ValidationError
            raise ValidationError("Restaurant not found or does not belong to you")
        
        serializer.save(restaurant=restaurant)
