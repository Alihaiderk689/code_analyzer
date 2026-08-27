"""Bulk revocation of a user's outstanding refresh tokens.

Lives here rather than in cookies.py because it's about server-side token
state, not about the cookies that happen to carry it: clearing a cookie only
tells one cooperating browser to forget a token, while blacklisting is what
actually makes that token stop working everywhere - including for an attacker
who already copied it out of a session that's being revoked.
"""
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken


def revoke_all_refresh_tokens(user):
    """Blacklists every refresh token ever issued to `user` that isn't already
    blacklisted, and returns how many were newly revoked.

    Relies on rest_framework_simplejwt.token_blacklist being installed (it is,
    see INSTALLED_APPS) - that app is what records an OutstandingToken row for
    each RefreshToken.for_user() call in the first place. Expired tokens are
    swept up too: they're already rejected on their `exp` claim, so
    blacklisting them changes nothing, and filtering them out would cost a
    second condition for no benefit.
    """
    unrevoked = OutstandingToken.objects.filter(user=user, blacklistedtoken__isnull=True)
    created = BlacklistedToken.objects.bulk_create(
        [BlacklistedToken(token=token) for token in unrevoked],
        # A concurrent logout/refresh of the same token can blacklist it
        # between the query above and this insert; that's the outcome we
        # wanted anyway, so it must not turn into an IntegrityError.
        ignore_conflicts=True,
    )
    return len(created)
