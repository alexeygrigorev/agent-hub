"""Installer release pins (2026-10-29).

Node: https://nodejs.org/dist/v22.23.4/SHASUMS256.txt
aplexer: PocketShell-io/aplexer v0.1.22/v0.1.23 SHA256SUMS.txt
Python wheels: versioned pypi.org JSON metadata (URLs and SHA256).
Antigravity: official per-platform updater manifests, pinned here for review.
Grok package identity: https://docs.x.ai/build/enterprise
Pins change with source review; the installer never trusts a mutable checksum
manifest or npm latest tag as its content pin.
"""

NODE_SHA256 = {'node-v22.23.4-darwin-arm64.tar.gz': 'c9e1dbc748b33ed2d14f42445191ecaf14c56259c7a9a26d0042c49ec2b66428',
 'node-v22.23.4-darwin-x64.tar.gz': 'efcc2b0db6f94bf53eddb038fe2b370277a042bd29e4f19b8f8e23598795b819',
 'node-v22.23.4-linux-arm64.tar.xz': '6287aa46b6c375d816e63ae66d024fbbfd70404825aabdfdf898a970871d2f4b',
 'node-v22.23.4-linux-x64.tar.xz': '1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce',
 'node-v22.23.4-win-arm64.zip': 'a8b8cd79cbbb3062435f449591d98dd63730bbbf30ce2d7cc3a6376c236cb111',
 'node-v22.23.4-win-x64.zip': 'cbc74b5e0982c9dbb41525fd8fb5dcd32fbee237863f7829072f4639a1ce5f20'}

APLEXER_SHA256 = {'0.1.22': {'aplexer-linux-amd64': 'cc8f79b9f34143cc1d38ba14410cf3bd8aab0a27b94fd3801f051b11b663de9c',
            'aplexer-linux-arm64': 'fe4db9710bd3782d705e7601dcb17dd27642be0e2508a85ec87ecb308d9fdf29',
            'aplexer-macos-amd64': 'b588bea800a36b642ee69a3701095d5d0512c1a7f8c41ad5fdd04d8e9d6939ca',
            'aplexer-macos-arm64': '0d2a93df472949b28c6a1d5803d94923306e3b57806fe642c6eb5f76fa759be1',
            'aplexer-windows-amd64.exe': '3631316273833ddc284be464f1491b5aeb862c32fbeaed978d8f3e16f46f2a68',
            'aplexer-windows-arm64.exe': '303a68d42ef5c95a7496c3330d1af9f0bc23059e7f27cf60e09fbb42a7fe2b3a',
            'aplexer_client-0.1.22-cp311-abi3-macosx_11_0_arm64.whl': 'c76c08125d699d2bdad53fb61fbca70f3a0989fbbcba00d7bdee221f59b1e13c',
            'aplexer_client-0.1.22-cp311-abi3-macosx_11_0_x86_64.whl': '768511de030d2c47c48ebf6d2710370a4c1428a2ab087e32054823003c4c5d17'},
 '0.1.23': {'aplexer-linux-amd64': '33c9ab47694f073a61c90a3bb85c4cffb0ae100a9e56dfa540cce01267f9371f',
            'aplexer-linux-arm64': 'ea475c94396e42b3e32b47a56567397ea2502ee3ae5357a32fa445203c9917eb',
            'aplexer-windows-amd64.exe': 'bc21a0b61734461b870f02674e444885073e4c9077d476dcd0f2e09caa1235fe',
            'aplexer-windows-arm64.exe': '8c64e5b9ed794626d70b87aca3640f949b6a90c40d5736a3dec2191916a3aa03'}}

ANTIGRAVITY_RELEASES = {'darwin_amd64': {'sha512': '6e5c1f534d7c9d68cb90afc7cdce45984f927cb1c156e7c736d9a487159fee5cedbf2df7055ead65ba69da534cd0c4d883ba64593a012c69d1407b8aaf9629a6',
                  'url': 'https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.10-5501573669978112/darwin-x64/cli_mac_x64.tar.gz',
                  'version': '1.4.10'},
 'darwin_arm64': {'sha512': '21e18868b63a35555919586f13dedf8d590151b4f818e5eec5cc85a17fb22530f7dc705fdd627766373317123bafc0b6e0ec25a33d31f144c6014120ae60c6a8',
                  'url': 'https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.10-5501573669978112/darwin-arm/cli_mac_arm64.tar.gz',
                  'version': '1.4.10'},
 'linux_amd64': {'sha512': '3a14c21697e86349ce8402e0b1889cbd1d3a5f4e2cb4dc43aae1c48c404240c2ccff9f775eec277e1f759234a33ec026645c63ed22ab6f6ebede561eda7c38b6',
                 'url': 'https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.10-5501573669978112/linux-x64/cli_linux_x64.tar.gz',
                 'version': '1.4.10'},
 'linux_arm64': {'sha512': '0182c23882b174c6f2968ad88de43262692ec363a938031319131551dab41cc6aca573d28d264095db357e49881a211114afe9f974386ed37208760f401ba98c',
                 'url': 'https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.10-5501573669978112/linux-arm/cli_linux_arm64.tar.gz',
                 'version': '1.4.10'},
 'windows_amd64': {'sha512': '581d3fad72393696bf2a35b7e66bd1e4ebdf895baa4727107f1b318ddba9f076241cd568a170f287fa89beee829337e88ff36b00455919f0b0ab1bac33c3439d',
                   'url': 'https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.10-5501573669978112/windows-x64/cli_windows_x64.exe',
                   'version': '1.4.10'},
 'windows_arm64': {'sha512': '20263ca8219a45c4addb3fefb2a748148cd781dfe2b6294261ce48dcd4e95b5c4ad725dc89552e57515a7db2401a1c2fc3fa26e56d42e2029e13de5d940a8bc1',
                   'url': 'https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.10-5501573669978112/windows-arm/cli_windows_arm64.exe',
                   'version': '1.4.10'}}

ENGINE_PACKAGES = {'claude': '@anthropic-ai/claude-code@2.1.312',
 'codex': '@openai/codex@0.166.0',
 'gemini': '@google/gemini-cli@0.66.0',
 'grok': '@xai-official/grok@1.0.70',
 'opencode': 'opencode-ai@1.19.5'}

PYTHON_WHEELS = {'aplexer-client': {'requires_dist': None,
                    'wheels': {'aplexer_client-0.1.23-cp311-abi3-manylinux_2_28_aarch64.whl': {'sha256': '50bf9639701208370d67888233195f76e9a9451744044fc62d35474a3ff02062',
                                                                                               'url': 'https://files.pythonhosted.org/packages/47/7f/01065af0aa8437b5e56638401299e0ed07ac17327278883bf95b365425ab/aplexer_client-0.1.23-cp311-abi3-manylinux_2_28_aarch64.whl'},
                               'aplexer_client-0.1.23-cp311-abi3-manylinux_2_28_x86_64.whl': {'sha256': 'def53ad61f824ff52061319b9ea39fb394ca134c5de2a959de9b8808620fdff6',
                                                                                              'url': 'https://files.pythonhosted.org/packages/4b/81/afe1ed3644f1cc1a5da283c5057c45d8adbbe34b83a23a02fb2019d17061/aplexer_client-0.1.23-cp311-abi3-manylinux_2_28_x86_64.whl'},
                               'aplexer_client-0.1.23-cp311-abi3-win_amd64.whl': {'sha256': '03e094916e805a664b16a0758d3aa6635f42b787e6d74f8f2c54faf312eec281',
                                                                                  'url': 'https://files.pythonhosted.org/packages/ff/a0/17081580073347b1a5a3420522ee014191e8f92e3325a8638c39612072f5/aplexer_client-0.1.23-cp311-abi3-win_amd64.whl'},
                               'aplexer_client-0.1.23-cp311-abi3-win_arm64.whl': {'sha256': 'fd4042d1b88f6a2c189dbd99e1a225311f20480126ddb3ca7fa1fb84b7589ca3',
                                                                                  'url': 'https://files.pythonhosted.org/packages/ba/a2/7da72a785a7cf25e3d4c8d52d0c5b875fba423ca27f685d313513cfcc101/aplexer_client-0.1.23-cp311-abi3-win_arm64.whl'}}},
 'click': {'requires_dist': ['colorama; platform_system == "Windows"'],
           'wheels': {'click-8.2.0-py3-none-any.whl': {'sha256': '6b303f0b2aa85f1cb4e5303078fadcbcd4e476f114fab9b5007005711839325c',
                                                       'url': 'https://files.pythonhosted.org/packages/a2/58/1f37bf81e3c689cc74ffa42102fa8915b59085f54a6e4a80bc6265c0f6bf/click-8.2.0-py3-none-any.whl'}}},
 'colorama': {'requires_dist': None,
              'wheels': {'colorama-0.4.6-py2.py3-none-any.whl': {'sha256': '4f1d9991f5acc0ca119f9d443620b77f9d6b33703e51011c16baf57afb285fc6',
                                                                 'url': 'https://files.pythonhosted.org/packages/d1/d6/3965ed04c63042e047cb6a3e6ed1a63a35087b6a609aa3a15ed8ac56c221/colorama-0.4.6-py2.py3-none-any.whl'}}},
 'quse': {'requires_dist': ['click>=8.2.0'],
          'wheels': {'quse-0.0.17-py3-none-any.whl': {'sha256': '4baff11a89650db44d89908fd43cdac3357f369a062a9f6eb0f7d581f62884ea',
                                                      'url': 'https://files.pythonhosted.org/packages/64/6c/57797a165d2a27d3f9cd1e6d2a7e07dfe582b6cfc8ed6209376c22e14ff5/quse-0.0.17-py3-none-any.whl'}}}}
