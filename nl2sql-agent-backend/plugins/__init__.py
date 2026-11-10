from abc import ABCMeta, abstractmethod, ABC

from metaclasses import SingletonMeta


class Singleton(metaclass=SingletonMeta):
    pass


plugin_classes = []


class PluginMeta(SingletonMeta, ABCMeta):
    def __new__(cls, name, bases, attrs):
        sub_cls = super().__new__(cls, name, bases, attrs)
        if name != "Plugin":
            plugin_classes.append(sub_cls)
        return sub_cls


class Plugin(ABC, metaclass=PluginMeta):
    @abstractmethod
    async def register(self) -> None:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass


plugins = []


async def register_plugins():
    for plugin_cls in plugin_classes:
        plugin = plugin_cls()
        await plugin.register()
        plugins.append(plugin)


async def close_plugins():
    for plugin in plugins:
        await plugin.close()
