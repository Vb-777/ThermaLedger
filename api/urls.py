from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("sites", views.SiteViewSet, basename="site")
router.register("meters", views.MeterViewSet, basename="meter")
router.register("invoices", views.InvoiceViewSet, basename="invoice")
router.register("contracts", views.ContractViewSet, basename="contract")

urlpatterns = [
    path("auth/token", obtain_auth_token, name="obtain-token"),
    path("me", views.me, name="me"),
    path("summary", views.summary, name="summary"),
    path("consumption", views.ConsumptionView.as_view(), name="consumption"),
    path("", include(router.urls)),
]
