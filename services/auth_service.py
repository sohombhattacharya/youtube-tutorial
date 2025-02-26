import os
import logging
import requests
from authlib.oauth2.rfc7523 import JWTBearerTokenValidator
from authlib.jose.rfc7517.jwk import JsonWebKey
from authlib.integrations.flask_oauth2 import ResourceProtector
from functools import wraps

class Auth0JWTBearerTokenValidator(JWTBearerTokenValidator):
    def __init__(self, domain, audience):
        logging.info(f"Initializing Auth0JWTBearerTokenValidator with domain: {domain} and audience: {audience}")
        issuer = f'https://{domain}/'
        jsonurl = requests.get(f'{issuer}.well-known/jwks.json')
        public_key = JsonWebKey.import_key_set(jsonurl.json())
        super().__init__(public_key, issuer=issuer, audience=audience)
        self.claims_options = {
            "exp": {"essential": True},
            "aud": {"essential": True, "value": audience},
            "iss": {"essential": True, "value": issuer},
            "sub": {"essential": True}
        }

def public_endpoint(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        return f(*args, **kwargs)
    setattr(decorated, '_public_endpoint', True)
    return decorated

class CustomResourceProtector(ResourceProtector):
    def acquire_token(self):
        # Check if endpoint is marked as public
        if hasattr(request.endpoint, '_public_endpoint'):
            return None
        return super().acquire_token()

# Update the require_auth to use the custom protector
require_auth = CustomResourceProtector()

def setup_auth(app):
    AUTH0_DOMAIN = os.getenv('AUTH0_DOMAIN')
    if not AUTH0_DOMAIN:
        logging.error("AUTH0_DOMAIN environment variable is not set!")
        raise ValueError("AUTH0_DOMAIN must be configured")

    auth0_validator = Auth0JWTBearerTokenValidator(
        AUTH0_DOMAIN,
        os.getenv('AUTH0_AUDIENCE')
    )
    
    require_auth.register_token_validator(auth0_validator)
    return require_auth

# Create the auth0_validator object with environment variables
AUTH0_DOMAIN = os.getenv('AUTH0_DOMAIN')
AUTH0_AUDIENCE = os.getenv('AUTH0_AUDIENCE')
if AUTH0_DOMAIN and AUTH0_AUDIENCE:
    auth0_validator = Auth0JWTBearerTokenValidator(AUTH0_DOMAIN, AUTH0_AUDIENCE)
else:
    logging.warning("AUTH0_DOMAIN or AUTH0_AUDIENCE not set. Authentication will not work properly.")
    auth0_validator = None