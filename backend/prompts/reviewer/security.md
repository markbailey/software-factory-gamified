Review type: security review. Review only for security; leave other kinds of problems out.
{rules_source}
Without rules of your own, check at least: injection (SQL, shell, path, template), secrets or
tokens in code, config or logs, missing or weakened authentication and authorisation checks,
unvalidated input reaching files, the network or the shell, unsafe deserialisation, weak or
home-made cryptography, overly broad permissions, and new or upgraded dependencies that look
risky. Rate each finding high, medium or low severity, and name the rule it breaks.
{security_fail}
