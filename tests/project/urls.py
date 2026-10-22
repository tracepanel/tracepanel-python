from django.urls import URLPattern, URLResolver, include, path

from tests.project import views

try:
    from strawberry.django.views import GraphQLView

    from tests.project.graphql import schema

    graphql = [path("graphql/", GraphQLView.as_view(schema=schema))]
except ImportError:  # the strawberry test group is optional
    graphql: list[URLPattern] = []

try:
    from tests.project.ninja import api

    ninja = [path("ninja/", api.urls)]
except ImportError:  # the ninja test group is optional
    ninja: list[URLResolver] = []

try:
    from tests.project import drf

    rest = [
        path("drf/", include(drf.router.urls)),
        path("drf/groups/", drf.GroupCreate.as_view()),
        path("drf/throttled/", drf.Throttled.as_view()),
        path("drf/whoami/", drf.whoami),
    ]
except ImportError:  # the drf test group is optional
    rest: list[URLPattern | URLResolver] = []

urlpatterns = [
    path("n-plus-one/", views.n_plus_one),
    path("busy/", views.busy),
    path("fixed/", views.fixed),
    path("duplicates/", views.duplicates),
    path("cache/", views.cached),
    path("logs/", views.logs),
    path("error/", views.error),
    path("chained-error/", views.chained_error),
    path("me/", views.me),
    path("untouched-user/", views.untouched_user),
    path("html/", views.html),
    path("users-page/", views.users_page),
    path("context-page/", views.context_page),
    path("multi-backend/", views.multi_backend),
    path("welcome-email/", views.welcome_email),
    path("create-group/", views.create_group),
    path("log-in/", views.log_in),
    path("same-group/", views.same_group),
    path("health/", views.health),
    path("instrumented/", views.instrumented),
    path("enqueue/", views.enqueue),
    path("run-eager/", views.run_eager),
    path("async/users/", views.async_users),
    path("async/error/", views.async_error),
    path("async/n-plus-one/", views.async_n_plus_one),
    path("place-order/", views.place_order),
    path("html-page/", views.html_page),
    path("streamed-page/", views.streamed_page),
    path("async-streamed-page/", views.async_streamed_page),
    path("htmx/row/", views.htmx_row),
    path("htmx/login-needed/", views.htmx_login_needed),
    path("django-tasks/<str:kind>/", views.django_task),
    path("caught-and-logged/", views.caught_and_logged),
    path("sign-out/", views.sign_out),
    path("echo-json/", views.echo_json),
    path("upload/", views.upload),
    path("catalog/", views.catalog),
    path("export/", views.export),
    path("remember/", views.remember),
    path("", views.home),
    *graphql,
    *ninja,
    *rest,
]
