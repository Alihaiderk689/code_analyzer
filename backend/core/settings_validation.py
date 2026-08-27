"""Startup validation for settings that are only dangerous when wrong in
production.

Same intent as the ENVIRONMENT check inlined at the top of config/settings.py:
a misconfiguration that silently downgrades security should stop the process
at boot, loudly, rather than serve traffic in a weakened state. Kept in its
own module (rather than inlined too) so the rules are directly unit-testable
without re-importing the settings module under a doctored environment.
"""
from django.core.exceptions import ImproperlyConfigured


def validate_allowed_hosts(hosts, environment):
    """Returns `hosts` unchanged, or raises ImproperlyConfigured.

    Only enforced for environment == 'production'. Development deliberately
    keeps Django's own behavior (DEBUG=True implicitly allows localhost), so
    a developer never has to configure this to run the server locally.

    Two rejected cases:

    - ``['*']`` - Django's blanket wildcard. It turns off Host header
      validation entirely, which is what stands between this app and
      Host-header poisoning: a request carrying an attacker-controlled Host
      reaches Django, and anything built from it (most importantly the
      password-reset links in accounts/emails.py) can be pointed at the
      attacker's domain. It is a tempting thing to set when a deploy 400s, so
      it is worth failing on explicitly rather than trusting review to catch it.
    - empty - with DEBUG=False, an empty ALLOWED_HOSTS rejects *every* request,
      so the app boots fine and then 400s on all traffic. Failing at startup
      names the actual cause instead.
    """
    if environment != 'production':
        return hosts

    if not hosts:
        raise ImproperlyConfigured(
            'ALLOWED_HOSTS must be set in production. Provide a comma-separated '
            'list of the hostnames this deployment serves, e.g. '
            'ALLOWED_HOSTS=api.example.com,example.com'
        )

    if '*' in hosts:
        raise ImproperlyConfigured(
            "ALLOWED_HOSTS may not contain '*' in production - that disables Host "
            'header validation entirely. List the hostnames this deployment '
            "actually serves instead. To match a domain and all its subdomains, "
            "use a leading dot ('.example.com'), which stays an explicit allowlist."
        )

    return hosts
