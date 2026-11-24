"""Whitelisted API methods, addressable at /api/method/<dotted.name> like Frappe."""

import inspect

METHODS = {}


def whitelist(*names, allow_guest=False, methods=("GET", "POST")):
	def decorator(func):
		func._allow_guest = allow_guest
		func._http_methods = set(methods)
		for name in names:
			METHODS[name] = func
		return func

	return decorator


def call(func, ctx, kwargs):
	"""Call a method with only the arguments it accepts (Frappe drops unknown form keys too)."""
	signature = inspect.signature(func)
	params = list(signature.parameters.values())[1:]  # skip ctx
	if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
		return func(ctx, **kwargs)
	accepted = {p.name for p in params}
	return func(ctx, **{key: value for key, value in kwargs.items() if key in accepted})
