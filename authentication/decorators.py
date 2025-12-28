from django.shortcuts import redirect

def institution_login_required(view_func):
    def wrapper(request, *args, **kwargs):
        if "role" not in request.session:
            return redirect("index")  # login page
        return view_func(request, *args, **kwargs)
    return wrapper
