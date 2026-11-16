"""Current real Node command, not a server/HTTP implementation or proxy."""
from pathlib import Path


def node_command(repo_root, data_dir, port, node_binary):
    entry=Path(repo_root)/'hub2/node/main.ts'
    if not entry.is_file():raise AssertionError('maintained Node entry is required')
    if type(port) is not int or not 1024<port<65536:raise ValueError('fixture port')
    if not Path(data_dir).is_absolute():raise ValueError('fixture data directory')
    return [str(node_binary),'--experimental-transform-types',str(entry),
            '--host','127.0.0.1','--port',str(port),'--data-dir',str(data_dir),
            '--dev-vars','none']
