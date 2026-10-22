"""The URLs of the projects without Django's optional apps: nothing here may import them."""

from django.db import connection
from django.http import HttpRequest, HttpResponse
from django.urls import path


def ping(request: HttpRequest) -> HttpResponse:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return HttpResponse("pong")


urlpatterns = [path("ping/", ping)]
