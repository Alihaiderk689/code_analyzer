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
import re

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

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

# Media (user-uploaded avatars) is served by Django in EVERY environment.
#
# This was previously DEBUG-gated, on the assumption that a reverse proxy would
# always serve it. That holds for Docker Compose (nginx serves /media/ from the
# shared media_data volume and never reaches these patterns) but NOT for the
# Render deployment, where there is no nginx in front of gunicorn - so with
# DEBUG=False every avatar URL 404'd while uploads kept returning 201.
#
# django.views.static.serve is not the fastest way to serve a file (no caching
# layer, path resolved per request) but it is safe - it uses safe_join, so the
# ../ traversal class of bug does not apply - and avatars are small, public,
# and rarely fetched. When a CDN or object store is introduced this route
# should go away again; until then a slow avatar beats a broken one.
# NOTE: django.conf.urls.static.static() is itself a no-op when DEBUG is False
# (it returns [] - that is the whole DEBUG gate, not the `if` that used to be
# here), so it cannot be used for this. The pattern is registered directly.
urlpatterns += [
    re_path(r'^%s(?P<path>.*)$' % re.escape(settings.MEDIA_URL.lstrip('/')), serve, {
        'document_root': settings.MEDIA_ROOT,
    }),
]
