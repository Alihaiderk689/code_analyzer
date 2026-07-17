from django.urls import path

from . import search_views as views

urlpatterns = [
    path('', views.SearchView.as_view(), name='search'),
]
