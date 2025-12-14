import yaml
from yaml.loader import SafeLoader


class NoDupLoader(SafeLoader):
    pass


def _construct_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"Duplicate key {key!r} in {node.start_mark}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


NoDupLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
