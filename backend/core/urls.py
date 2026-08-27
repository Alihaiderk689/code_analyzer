from django.urls import path

from . import views

urlpatterns = [
    # Liveness - what render.yaml's healthCheckPath points at. See core/views.py.
    path('health/', views.health_check, name='health-check'),
    # Readiness - probes the database (and reports cache state). For monitoring
    # and manual diagnosis, deliberately NOT the platform restart trigger.
    path('health/ready/', views.readiness_check, name='readiness-check'),
]
