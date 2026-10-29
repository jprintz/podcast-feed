"""The web application: the public feed and media routes, the admin UI and its static files."""

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from podcast_feed.service import Service
from podcast_feed.web_admin import SameOriginMiddleware, admin_routes
from podcast_feed.web_public import public_routes


def create_web_app(service: Service) -> Starlette:
    """One app for both: the internet reaches `/feeds/` and `/media/` through the gateway, the
    admin reaches `/ui/` through Cloudflare Access. The same-origin check covers all of it."""

    async def root(request: Request) -> Response:
        return RedirectResponse("/ui/", status_code=303)

    app = Starlette(
        routes=[
            Route("/", root, methods=["GET"]),
            *public_routes(service),
            *admin_routes(service),
            Mount("/ui/static", StaticFiles(packages=[("podcast_feed", "static")])),
        ],
        middleware=[Middleware(SameOriginMiddleware)],
    )
    # A redirect to the slashed path would carry the app's internal host name to the browser.
    # Without it `/feeds/talks.xml/` and `/ui` are plain 404s.
    app.router.redirect_slashes = False
    return app
