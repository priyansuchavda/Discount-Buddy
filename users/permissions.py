from rest_framework.permissions import BasePermission, SAFE_METHODS

from .models import UserProfile


def has_merchant_instance(user):
    """Check if user has a Merchant instance"""
    try:
        # Accessing a OneToOne reverse relation that doesn't exist raises RelatedObjectDoesNotExist
        return user.merchant is not None
    except Exception:
        # RelatedObjectDoesNotExist or any other exception means no Merchant instance
        return False


class IsAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "profile", None)
            and request.user.profile.role == UserProfile.ROLE_ADMIN
        )


class IsMerchant(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "profile", None)
            and request.user.profile.role == UserProfile.ROLE_MERCHANT
            and has_merchant_instance(request.user)
        )


class IsCustomer(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "profile", None)
            and request.user.profile.role == UserProfile.ROLE_CUSTOMER
        )


class ReadOnly(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS


