import unittest
from hub2_agent_hub_host.native_launch import adapter_argv, interactive_argv

class ArgonInteractivePromptTests(unittest.TestCase):
    def test_interactive_prompt_uses_supported_flag_and_preserves_model_effort(self):
        prompt='Live smoke brief\nFirst action: write your own identity.'
        headless=adapter_argv('antigravity',prompt,'gemini-4-argon-medium')
        argv=interactive_argv('antigravity',headless,prompt,model='gemini-4-argon-medium',effort='medium')
        self.assertEqual(argv[-2:],['--prompt-interactive',prompt])
        self.assertNotIn('-p',argv)
        self.assertEqual(argv[argv.index('--model')+1],'gemini-4-argon-medium')
        self.assertEqual(argv[argv.index('--effort')+1],'medium')
        self.assertEqual(headless[-2:],['-p',prompt])

    def test_claude_interactive_keeps_existing_positional_prompt(self):
        prompt='Claude prompt'
        argv=interactive_argv('claude',adapter_argv('claude',prompt,'sonnet'),prompt,model='sonnet')
        self.assertEqual(argv[-1],prompt)
        self.assertNotIn('--prompt-interactive',argv)
