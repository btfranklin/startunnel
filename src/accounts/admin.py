"""Keep the admin login under the same limit as the product login."""

from django.contrib import admin

from .forms import ThrottledAdminAuthenticationForm

admin.site.login_form = ThrottledAdminAuthenticationForm
