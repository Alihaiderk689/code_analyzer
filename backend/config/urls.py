"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('core.urls')),
    path('api/auth/', include('accounts.urls')),
    path('api/users/', include('accounts.profile_urls')),
    path('api/dashboard/', include('analyses.urls')),
    path('api/analysis/', include('analyses.analysis_urls')),
    path('api/ai/', include('ai.urls')),
    path('api/history/', include('analyses.history_urls')),
    path('api/reports/', include('analyses.report_urls')),
    path('api/search/', include('analyses.search_urls')),
    path('api/admin/', include('adminapi.urls')),
    path('api/chat/', include('chat.urls')),
    path('api/github/', include('github_integration.urls')),
    path('api/webhooks/', include('github_integration.webhook_urls')),
]

# DEBUG-only, same as Django's default: this view exists for local `runserver`
# convenience, not production use (no caching, does its own path resolution
# per request). In Docker Compose, media isn't served by Django at all - nginx
# serves it directly from a volume shared with this container (see
# frontend/nginx.conf's /media/ location + docker-compose.yml's media_data
# volume), which is what actually serves it in every non-DEBUG deployment.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
