"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
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
from django.contrib import admin
from django.urls import include, path

from mindcare.api.swagger import openapi_schema, swagger_ui

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/openapi.json', openapi_schema, name='openapi-schema'),
    path('api/v1/docs/', swagger_ui, name='swagger-ui'),
    path('api/v1/', include('mindcare.api.urls')),
]
