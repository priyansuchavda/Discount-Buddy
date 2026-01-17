from django.utils import timezone
from django.core.cache import cache
from rest_framework import generics, permissions, filters

from users.permissions import ReadOnly, IsMerchant
from .models import Voucher
from .serializers import VoucherSerializer


class VoucherListView(generics.ListAPIView):
    serializer_class = VoucherSerializer
    permission_classes = [permissions.AllowAny]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["code", "title", "merchant__name"]
    ordering_fields = ["start_date", "end_date", "sale_price", "discount_percent"]

    def get_queryset(self):
        now = timezone.now()
        cache_key = f"voucher_list_active_{now.date()}"
        qs = cache.get(cache_key)
        if qs is None:
            qs = (
                Voucher.objects.filter(
                    end_date__gte=now, is_active=True, merchant__verified=True
                )
                .select_related("merchant", "category")
                .order_by("-created_at")
            )
            cache.set(cache_key, qs, 60)  # cache 1 minute
        return qs


class MerchantVoucherView(generics.ListCreateAPIView):
    serializer_class = VoucherSerializer
    permission_classes = [IsMerchant | ReadOnly]

    def get_queryset(self):
        return (
            Voucher.objects.filter(merchant__user=self.request.user)
            .select_related("merchant", "category")
            .order_by("-created_at")
        )

    def perform_create(self, serializer):
        try:
            merchant = self.request.user.merchant
        except (AttributeError, Exception):
            # RelatedObjectDoesNotExist or any other exception
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Merchant profile not found. Please create a merchant account.")
        serializer.save(merchant=merchant)


