"""Owner-facing slash commands: user-only skills that run the matching subcommand of their plugin's CLI.
Run: python3 tests/test_owner_commands.py"""
import os
import re

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
for plugin, name, sub in (("tokenmaxxing", "report", "report"), ("tokenmaxxing", "recall", "recall"),
                          ("tokenmaxxing", "init", "init"), ("gutcheck", "init", "init")):
    root = os.path.join(REPO, plugin)
    text = open(os.path.join(root, "skills", name, "SKILL.md")).read()
    fm = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
    assert re.search(r"^name: " + name + "$", fm, re.M), name
    assert re.search(r"^disable-model-invocation: true$", fm, re.M), name  # stays out of the model's context
    assert re.search(r"^description: .+", fm, re.M), name
    cmd = re.search(r"```!\n(.*?)\n```", text, re.S).group(1)
    assert cmd.startswith(f'"${{CLAUDE_SKILL_DIR}}/../../bin/{plugin}" ' + sub), cmd
    assert os.access(os.path.join(root, "skills", name, "..", "..", "bin", plugin), os.X_OK)
recall = open(os.path.join(REPO, "tokenmaxxing", "skills", "recall", "SKILL.md")).read()
assert "<<'GUTCHECK_QUESTION'\n$ARGUMENTS\nGUTCHECK_QUESTION" in recall  # quotes and $ in the question stay literal
print("owner commands ok")
