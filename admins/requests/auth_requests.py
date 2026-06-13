from base.requests.base import validate_request


def login_request(request):
    return validate_request(request, ['email', 'password'])


def change_password_request(request):
    return validate_request(request, ['current_password', 'new_password'])


def revoke_session_request(request):
    return validate_request(request, ['session_id'])
