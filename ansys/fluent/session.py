import grpc
import atexit

from ansys.api.fluent.v0 import datamodel_pb2_grpc as DataModelGrpcModule
from ansys.fluent.core.core import DatamodelService

def parse_server_info_file(filename: str):
    with open(filename, "rb") as f:
        lines = f.readlines()
    return lines[0].strip(), lines[1].strip()

class Session:
    __all_sessions = []

    def __init__(self, server_info_filepath):
        address, password = parse_server_info_file(server_info_filepath)
        self.__channel = grpc.insecure_channel(address)
        datamodel_stub = DataModelGrpcModule.DataModelStub(self.__channel)
        self.service = DatamodelService(datamodel_stub, password)
        self.tui = Session.tui(self.service)
        Session.__all_sessions.append(self)

    def exit(self):
        if self.__channel:
            self.tui.exit()
            self.__channel.close()
            self.__channel = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.exit()

    @staticmethod
    def exit_all():
        for session in Session.__all_sessions:
            session.exit()

    class tui:
        __application_modules = []

        def __init__(self, service):
            self.service = service
            for mod in self.__class__.__application_modules:
                for name, cls in mod.__dict__.items():
                    if cls.__class__.__name__ == 'PyMenuMeta':
                        setattr(self, name, cls([(name, None)], service))
                    if cls.__class__.__name__ in 'PyNamedObjectMeta':
                        setattr(self, name, cls([(name, None)], None, service))

        @classmethod
        def register_module(cls, mod):
            cls.__application_modules.append(mod)
            for name, obj in mod.__dict__.items():
                if callable(obj):
                    setattr(cls, name, obj)

atexit.register(Session.exit_all)