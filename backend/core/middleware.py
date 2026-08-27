"""Response security headers Django's own SecurityMiddleware doesn't cover.

SecurityMiddleware already handles HSTS, nosniff and Referrer-Policy from
settings, and XFrameOptionsMiddleware handles X-Frame-Options - those stay
where they are. What has no settings equivalent in Django 4.2 is
Content-Security-Policy and Permissions-Policy, which is all this adds.

Two CSPs, picked by response content type:

- Non-HTML responses (i.e. essentially every /api/ response, all of which are
  JSON - see core/exceptions.py, which guarantees even unhandled errors come
  back as JSON) get the maximally strict policy. A JSON document loads no
  subresources at all, so `default-src 'none'` costs nothing and shuts down
  the whole class of "browser was talked into rendering this API response as
  a document" tricks.
- HTML responses get a policy that still forbids framing but permits the
  same-origin assets those pages genuinely need. The only HTML this backend
  serves is Django admin (WhiteNoise-served CSS/JS plus admin's own inline
  theme-toggle script), DRF's browsable API, and the analysis HTML report
  (analyses/templates/analyses/report.html, one inline <style> block) - a
  `default-src 'none'` policy would visibly break all three, which is why
  they get their own policy instead of one blanket rule.

`frame-ancestors 'none'` is set in both, and is the reason this has to be an
HTTP header at all: the frontend's build-time CSP <meta> tag
(frontend/vite.config.js) cannot express frame-ancestors - browsers ignore
that directive in a <meta> - so clickjacking protection for API responses has
to come from here. It duplicates X-Frame-Options: DENY on purpose; the two
are redundant only in browsers that support both.
"""

# form-action 'none' rather than 'self': nothing this backend returns as
# non-HTML is a document with a form in it, so there is no legitimate
# submission target to allow.
API_CSP = (
    "default-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)

# 'unsafe-inline' is load-bearing here, not laziness: Django admin ships an
# inline <script> for its light/dark theme toggle and inline style attributes,
# and the report template carries an inline <style> block. Narrowing this
# means nonce-ing those templates, which is a change to admin's own HTML.
HTML_CSP = (
    "default-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "connect-src 'self'"
)

# Opts this origin out of browser features it has no use for, so a future XSS
# (or an embedded third-party frame) can't prompt the user for them under this
# origin's name. `()` is the current syntax for "no origin, not even self".
PERMISSIONS_POLICY = (
    'accelerometer=(), '
    'autoplay=(), '
    'camera=(), '
    'display-capture=(), '
    'encrypted-media=(), '
    'fullscreen=(), '
    'geolocation=(), '
    'gyroscope=(), '
    'magnetometer=(), '
    'microphone=(), '
    'midi=(), '
    'payment=(), '
    'usb=(), '
    'xr-spatial-tracking=()'
)


class SecurityHeadersMiddleware:
    """Adds Content-Security-Policy and Permissions-Policy to every response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # setdefault, not assignment: a view that has deliberately set its own
        # policy (none do today) should keep it rather than be overwritten.
        response.setdefault('Content-Security-Policy', self._csp_for(response))
        response.setdefault('Permissions-Policy', PERMISSIONS_POLICY)
        return response

    @staticmethod
    def _csp_for(response):
        content_type = response.get('Content-Type', '')
        return HTML_CSP if content_type.startswith('text/html') else API_CSP
