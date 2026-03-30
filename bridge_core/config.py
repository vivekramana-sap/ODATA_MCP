"""
Configuration loader — reads services.json and creates ODataService instances.
"""

import json

from .helpers import expand_env
from .odata_service import ODataService


def load_services(config_path: str, cli_args) -> list:
    with open(config_path) as fh:
        cfg = json.load(fh)

    services: list = []
    for svc_cfg in cfg:
        alias      = svc_cfg.get("alias", "svc")
        url        = expand_env(svc_cfg.get("url", ""))
        username   = expand_env(svc_cfg.get("username", ""))
        password   = expand_env(svc_cfg.get("password", ""))
        passthrough        = svc_cfg.get("passthrough",            False)
        passthrough_header = svc_cfg.get("passthrough_header",    "")
        include            = svc_cfg.get("include",                None)
        readonly           = svc_cfg.get("readonly",               False)
        robf               = svc_cfg.get("readonly_but_functions", False)
        include_actions    = svc_cfg.get("include_actions",        None)
        enable_ops         = svc_cfg.get("enable_ops",  getattr(cli_args, "enable",  ""))
        disable_ops        = svc_cfg.get("disable_ops", getattr(cli_args, "disable", ""))
        default_top        = svc_cfg.get("default_top", 50)
        max_top            = svc_cfg.get("max_top",     500)
        cookie_file        = svc_cfg.get("cookie_file",   getattr(cli_args, "cookie_file",   ""))
        cookie_string      = svc_cfg.get("cookie_string", getattr(cli_args, "cookie_string", ""))
        group              = svc_cfg.get("group",          "")

        if getattr(cli_args, "read_only",               False):
            readonly = True
        if getattr(cli_args, "read_only_but_functions", False):
            robf     = True

        services.append(ODataService(
            alias                  = alias,
            url                    = url,
            username               = username,
            password               = password,
            passthrough            = passthrough,
            passthrough_header     = passthrough_header,
            include                = include,
            readonly               = readonly,
            readonly_but_functions = robf,
            include_actions        = include_actions,
            enable_ops             = enable_ops,
            disable_ops            = disable_ops,
            default_top            = default_top,
            max_top                = max_top,
            legacy_dates           = not getattr(cli_args, "no_legacy_dates",      False),
            cookie_file            = cookie_file,
            cookie_string          = cookie_string,
            verbose_errors         = getattr(cli_args, "verbose_errors",          False),
            max_items              = getattr(cli_args, "max_items",               100),
            max_response_size      = getattr(cli_args, "max_response_size", 5 * 1024 * 1024),
            group                  = group,
        ))
    return services
