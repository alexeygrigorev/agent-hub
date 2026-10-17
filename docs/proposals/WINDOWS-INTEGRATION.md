# Windows integration delta

Prepared for exact different-model review; not accepted, applied or integrated. CFB remains provisional while its independent Argon review is pending. Physical Windows DLL/ABI/NTFS and M5/M11 remain OPEN.

This executable delta composes the independently scoped-reviewed Windows host port into frozen conversation assembly `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc`, based on main `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24`. Windows input is `2de0a6c1846b110227cd1d15e99a7af6999158f5`, scoped source ACCEPT `3d0dfadbed35812aeaacbb98f304acfecf711e91`. Those component verdicts do not accept this composition.

Patch SHA-256: `bf2f3df7070c2c9145ca86dd4ada7e5ff6399c6e478284807e0d1009b5d61500`; exactly the reviewed Windows patch, with no adaptation. CFB patch SHA-256: `e8c91a9c3f3a795360c85eec300d3df9187a361d57f9a9dfc9312ad60e673d10`. Independently verified six existing input hashes and four NEW absences before private application. All 193 other CFB paths retain exact bytes; no API/auth/UI/F7/pairing/M6 or CLI behavior edit, M5/M8 import, or peer dirty input.

Full manifest algorithm: sorted `[path,sha256]` rows encoded as compact JSON, SHA-256 of UTF-8 bytes; exclude runtime/dependency/build output. Input:199 files, `17aab235373ff0f98cce9ff361b74333d9d3bd23bf45790d471865f3ea02223a`. Output:203 files, `5fb8eeace9f7b936e1b9043902cea3783cd0aa72decabedb51f96bef298e8804`.

Private reconstruction and manifests are retained under ignored `.local/windows/`. Only this report and adjacent delta are committed. Verification is in progress; no passing test or native proof is claimed at this checkpoint. Product source exists only in private Git-object reconstruction. Head owns Agent Branches fork/WIP/main, source leases, independent review, integration and private backup. No push/deploy/source application.

## Exact future owner scope and hashes

| Path | CFB input SHA-256 | Composed output SHA-256 |
| --- | --- | --- |
| `host/agent_hub_host/bridge.py` | `2b5e775dbbf194fcb619171a483b9b4152af8a2e76c46a6450b47ead8f9eb464` | `033db99502033296143522917d01bcf63073f41cd0e3bf4f32227187a017b5ea` |
| `host/agent_hub_host/config.py` | `2ee1abe6c62cf93d494cb0098525d69750f462cb2ec5024a8b5cacd8a8e29537` | `6cce08e8682e4b70b3d26231716dc1ceb4c041f618fd4fb7ecf13212e38999fa` |
| `host/agent_hub_host/conversation_bridge.py` | `7e7ebaec3148b4cb5522bdead67e171f9adbcbb8e6c0d94085813c80ee313a4e` | `747e3cf7ccd2296f89cd833c7a6864dfce7602f79bf50095e6b17cc3297b5855` |
| `host/agent_hub_host/native_bridge.py` | `b8f1f4dbac0093f48176542fad393342866c07348b7728198ec643ab0fce0b3f` | `528dab707ebd7ae12eb97d7db94a6cf17658108dfd1e16f7d1c77e89c5fc8ab9` |
| `host/agent_hub_host/private_fs.py` | `NEW` | `ef4b6436bb74c08b2e4e367f82bf5a02bd1db9e3d04f416b480b46f8c8843a18` |
| `host/agent_hub_host/win32_fs.py` | `NEW` | `34589f69dc0f11cabb79fd08afaf0c4a48b3a7e6ee2c2bf549b7066ade99c302` |
| `host/tests/test_bridge.py` | `783d38efeac5047c0c0182d073ef4e3b553526c3c4c29364c82531ecdecf51e5` | `dcb6dd44cd21753ef9e773df5aefc8ed2b60e30268cbdbff23444bbc73bf237c` |
| `host/tests/test_config.py` | `7d6fe1beab39bb10409f829458e1c7280be78870bb03f74c32b9859dc9940ab8` | `761b35077d66fc2c541de0d03409c693fd1ee65d0b17a85d8f1d1eef830c9399` |
| `host/tests/test_private_fs.py` | `NEW` | `bd56b0cf54df9a92a39430754e6bc15e6a1f7fc1e5cac580bd94dad3529cc1bd` |
| `host/tests/test_windows_imports.py` | `NEW` | `8a7d5bb02994c0558c13843f62823eb9d89c0e3eac5431e138c3e9291b06c52b` |
