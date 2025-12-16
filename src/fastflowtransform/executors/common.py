# fastflowtransform/executors/common.py


def _q_ident(ident: str) -> str:
    # Simple, safe quoting for identifiers
    return '"' + ident.replace('"', '""') + '"'
