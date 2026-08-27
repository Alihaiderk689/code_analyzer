from django.urls import path

from . import report_views as views

urlpatterns = [
    path('<int:analysis_id>/pdf/', views.PDFReportView.as_view(), name='report-pdf'),
    path('<int:analysis_id>/html/', views.HTMLReportView.as_view(), name='report-html'),
]
