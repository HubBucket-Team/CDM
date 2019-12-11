from collections import OrderedDict
import importlib
import json
import os
from typing import List, Optional, Tuple, Set, TYPE_CHECKING

from cdm.storage import GithubAdapter, LocalAdapter, ResourceAdapter

if TYPE_CHECKING:
    from cdm.objectmodel import CdmContainerDefinition, CdmCorpusDefinition, CdmFolderDefinition


class StorageManager:
    def __init__(self, corpus: 'CdmCorpusDefinition'):
        # the default namespace to be used when not specified.
        self.default_namespace = None  # type: Optional[str]

        # Internal

        self._corpus = corpus
        self._namespace_adapters = OrderedDict()  # type: Dict[str, StorageAdapterBase]
        self._namespace_folders = OrderedDict()  # type: Dict[str, CdmFolderDefinition]
        self._registered_adapter_types = {
            'local': 'LocalAdapter',
            'adls': 'ADLSAdapter',
            'remote': 'RemoteAdapter',
            'github': 'GithubAdapter'
        }

        # The namespaces that have default adapters defined by the program and not by a user.
        self._system_defined_namespaces = set()  # type: Set

        # set up default adapters.
        self.mount('local', LocalAdapter(root=os.getcwd()))
        self.mount('cdm', GithubAdapter())

        self._system_defined_namespaces.add('local')
        self._system_defined_namespaces.add('cdm')

    @property
    def _ctx(self):
        return self._corpus.ctx

    def adapter_path_to_corpus_path(self, adapter_path: str) -> Optional[str]:
        """Takes a storage adapter domain path, figures out the right adapter to use
        and then return a corpus path"""
        result = None

        # Keep trying adapters until one of them likes what it sees
        if self._namespace_adapters:
            for key, value in self._namespace_adapters.items():
                result = value.create_corpus_path(adapter_path)
                if result:
                    # Got one, add the prefix
                    result = '{}:{}'.format(key, result)
                    break

        if not result:
            self._ctx.logger.error('No registered storage adapter understood the path "{}"'.format(adapter_path),
                                   'adapter_path_to_corpus_path')

        return result

    def corpus_path_to_adapter_path(self, corpus_path: str) -> Optional[str]:
        """Takes a corpus path, figures out the right adapter to use and then return
        an adapter domain path"""
        result = None

        # Break the corpus path into namespace and ... path
        path_tuple = self.split_namespace_path(corpus_path)
        namespace = path_tuple[0] or self.default_namespace

        # Get the adapter registered for this namespace
        namespace_adapter = self.fetch_adapter(namespace)
        if not namespace_adapter:
            self._ctx.logger.error('The namespace "{}" has not been registered'.format(namespace))
        else:
            # Ask the storage adapter to 'adapt' this path
            result = namespace_adapter.create_adapter_path(path_tuple[1])

        return result

    def fetch_adapter(self, namespace: str) -> 'StorageAdapterBase':
        if namespace in self._namespace_adapters:
            return self._namespace_adapters[namespace]

        self._ctx.logger.error('Adapter not found for the namespace \'{}\''.format(namespace))
        return None

    def fetch_root_folder(self, namespace: Optional[str]) -> 'CdmFolderDefinition':
        """given the namespace of a registered storage adapter, return the root
        folder containing the sub-folders and documents"""
        if namespace and namespace in self._namespace_folders:
            return self._namespace_folders[namespace]

        self._ctx.logger.error('missing adapter for namespace "{}"'.format(namespace), 'fetch_root_folder')
        return None

    def fetch_config(self) -> str:
        """Generates the full adapters config."""
        adapters_array = []

        # Construct the JObject for each adapter.
        for namespace, adapter in self._namespace_adapters.items():
            # Skip system-defined adapters and resource adapters.
            if adapter._type == 'resource' or namespace in self._system_defined_namespaces:
                continue

            config = adapter.fetch_config()
            if not config:
                self._ctx.logger.error('JSON config constructed by adapter is null or empty.')
                continue

            json_config = json.loads(config)
            json_config['namespace'] = namespace

            adapters_array.append(json_config)

        result_config = {}

        # App ID might not be set.
        if self._corpus.app_id:
            result_config['appId'] = self._corpus.app_id

        result_config['defaultNamespace'] = self.default_namespace
        result_config['adapters'] = adapters_array

        return json.dumps(result_config)

    async def save_adapters_config_async(self, name: str, adapter: 'StorageAdapter') -> None:
        """Saves adapters config into a file."""
        await adapter.write_async(name, self.fetch_config())

    def create_absolute_corpus_path(self, object_path: str, obj: 'CdmObject' = None) -> Optional[str]:
        """Takes a corpus path (relative or absolute) and creates a valid absolute
        path with namespace"""
        if self._contains_unsupported_path_format(object_path):
            # Already called status_rpt when checking for unsupported path format.
            return None

        path_tuple = self.split_namespace_path(object_path)
        final_namespace = ''

        prefix = None
        namespace_from_obj = None

        if obj and hasattr(obj, 'namespace') and hasattr(obj, 'folder_path'):
            prefix = obj.folder_path
            namespace_from_obj = obj.namespace
        elif obj:
            prefix = obj.in_document.folder_path
            namespace_from_obj = obj.in_document.namespace

        if prefix and self._contains_unsupported_path_format(prefix):
            # Already called status_rpt when checking for unsupported path format.
            return None

        if prefix and prefix[-1] != '/':
            self._ctx.logger.warning('Expected path prefix to end in /, but it didn\'t. Appended the /', prefix)
            prefix += '/'

        # check if this is a relative path
        if path_tuple[1][0] != '/':
            if not obj:
                # relative path and no other info given, assume default and root
                prefix = '/'

            if path_tuple[0] and path_tuple[0] != namespace_from_obj:
                self._ctx.logger.warning('The namespace "{}" found on the path does not match the namespace found on the object'.format(path_tuple[0]))
                return None

            path_tuple = (path_tuple[0], prefix + path_tuple[1])
            final_namespace = namespace_from_obj or path_tuple[0] or self.default_namespace
        else:
            final_namespace = path_tuple[0] or namespace_from_obj or self.default_namespace

        return '{}:{}'.format(final_namespace, path_tuple[1]) if final_namespace else path_tuple[1]

    def create_relative_corpus_path(self, object_path: str, relative_to: Optional['CdmContainerDefinition'] = None):
        """Takes a corpus path (relative or absolute) and creates a valid relative corpus path with namespace.
            object_path: The path that should be made relative, if possible
            relative_to: The object that the path should be made relative with respect to."""
        new_path = self.create_absolute_corpus_path(object_path, relative_to)

        namespace_string = relative_to.namespace + ':' if relative_to and relative_to.namespace else ''
        if namespace_string and new_path.startswith(namespace_string):
            new_path = new_path[len(namespace_string):]

            if relative_to and relative_to.folder_path and new_path.startswith(relative_to.folder_path):
                new_path = new_path[len(relative_to.folder_path):]
        return new_path

    def mount(self, namespace: str, adapter: 'StorageAdapterBase') -> None:
        """registers a namespace and assigns creates a storage adapter for that namespace"""
        from cdm.objectmodel import CdmFolderDefinition

        if adapter:
            self._namespace_adapters[namespace] = adapter
            fd = CdmFolderDefinition(self._ctx, '')
            fd._corpus = self._corpus
            fd.namespace = namespace
            fd.folder_path = '/'
            self._namespace_folders[namespace] = fd
            if namespace in self._system_defined_namespaces:
                self._system_defined_namespaces.remove(namespace)

    def mount_from_config(self, adapter_config: str, does_return_error_list: bool = False) -> List['StorageAdapter']:
        if not adapter_config:
            self._ctx.logger.error('Adapter config cannot be null or empty.')
            return None

        adapter_config_json = json.loads(adapter_config)
        adapers_module = importlib.import_module('cdm.storage')

        if adapter_config_json.get('appId'):
            self._corpus.app_id = adapter_config_json['appId']

        if adapter_config_json.get('defaultNamespace'):
            self.default_namespace = adapter_config_json['defaultNamespace']

        unrecognized_adapters = []

        for item in adapter_config_json['adapters']:
            namespace = None
            # Check whether the namespace exists.
            if item.get('namespace'):
                namespace = item['namespace']
            else:
                self._ctx.logger.error('The namespace is missing for one of the adapters in the JSON config.')
                continue

            configs = None
            # Check whether the config exists.
            if item.get('config'):
                configs = item['config']
            else:
                self._ctx.logger.error('Missing JSON config for the namespace {}.'.format(namespace))
                continue

            if not item.get('type'):
                self._ctx.logger.error('Missing type in the JSON config for the namespace {}.'.format(namespace))
                continue

            adapter_type = self._registered_adapter_types.get(item['type'], None)

            if adapter_type is None:
                unrecognized_adapters.append(json.dumps(item))
            else:
                adapter = getattr(adapers_module, adapter_type)()
                adapter.update_config(json.dumps(configs))
                self.mount(namespace, adapter)

        return unrecognized_adapters if does_return_error_list else None

    def split_namespace_path(self, object_path: str) -> Tuple[str, str]:
        namespace = ''
        namespace_index = object_path.find(':')

        if namespace_index != -1:
            namespace = object_path[0: namespace_index]
            object_path = object_path[namespace_index + 1:]

        return (namespace, object_path)

    def unmount(self, namespace: str) -> None:
        """unregisters a storage adapter and its root folder"""

        self._namespace_adapters.pop(namespace, None)
        self._namespace_folders.pop(namespace, None)

        if namespace in self._system_defined_namespaces:
            self._system_defined_namespaces.remove(namespace)

        # The special case, use Resource adapter.
        if (namespace == 'cdm'):
            self.mount(namespace, ResourceAdapter())

    def _contains_unsupported_path_format(self, path: str) -> bool:
        """Checks whether the paths has an unsupported format, such as starting with ./ or containing ../ or /./
        In case unsupported path format is found, function calls status_rpt and returns True.
        Returns False if path seems OK.
        """
        if path.startswith('./') or path.startswith('.\\'):
            status_message = 'The path should not start with ./'
        elif '../' in path or '..\\' in path:
            status_message = 'The path should not contain ../'
        elif '/./' in path or '\\.\\' in path:
            status_message = 'The path should not contain /./'
        else:
            return False

        self._ctx.logger.error(status_message, path)
        return True
