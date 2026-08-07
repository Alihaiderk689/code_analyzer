"""Finds-or-creates a platform User from an already-verified GitHub identity.

Mirrors the find-or-create half of GoogleLoginSerializer (accounts/serializers.py),
but as a plain function rather than a DRF serializer: this is called directly by
github_integration's OAuth callback view with claims it already exchanged and
verified, not from a POST body that needs field-level validation.
"""
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from .serializers import _generate_username
from .validators import normalize_email

User = get_user_model()


class GitHubAccountNotEmailVerifiedError(Exception):
    """GitHub didn't return a verified primary email for this account - can't
    safely auto-link an existing account or create a new one without one."""


class GitHubAccountConflictError(Exception):
    """This GitHub identity is already linked to a different platform user
    (or vice versa) - surfaced cleanly rather than a raw IntegrityError."""


def find_or_create_user_from_github(claims: dict) -> User:
    github_id = str(claims['github_id'])

    user = User.objects.filter(profile__github_id=github_id).first()
    if user is not None:
        return user

    # First time we've seen this GitHub id - only auto-link/create off the
    # email claim if GitHub itself returned a verified primary email (see
    # GitHubClient.get_primary_verified_email); a returning user (matched by
    # github_id above) skips this check entirely, same asymmetry as Google's.
    email = claims.get('email')
    if not email:
        raise GitHubAccountNotEmailVerifiedError(
            "GitHub didn't return a verified email for this account. Verify an email on "
            "GitHub, or sign in with a password if you already have an account."
        )
    email = normalize_email(email)

    existing = User.objects.filter(email__iexact=email).first()
    if existing is not None:
        if existing.profile.github_id and existing.profile.github_id != github_id:
            raise GitHubAccountConflictError('This account is linked to a different GitHub account.')
        existing.profile.github_id = github_id
        if not existing.profile.is_verified:
            existing.profile.is_verified = True
        existing.profile.save(update_fields=['github_id', 'is_verified'])
        return existing

    try:
        with transaction.atomic():
            user = User(
                username=_generate_username(email),
                email=email,
            )
            user.set_unusable_password()
            user.save()
            user.profile.github_id = github_id
            user.profile.is_verified = True
            user.profile.save(update_fields=['github_id', 'is_verified'])
    except IntegrityError:
        # Concurrent duplicate first-login (e.g. a double-click) racing on the
        # github_id unique constraint.
        raise GitHubAccountConflictError('Something went wrong. Please try again.')
    return user
