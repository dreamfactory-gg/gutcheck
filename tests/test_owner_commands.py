"""Owner-facing slash commands: user-only skills that run the matching `gutcheck` subcommand.
Run: python3 tests/test_owner_commands.py"""
import os
import re

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gutcheck")
for name, sub in (("report", "report"), ("recall", "recall"), ("init", "init")):
    text = open(os.path.join(ROOT, "skills", name, "SKILL.md")).read()
    fm = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
    assert re.search(r"^name: " + name + "$", fm, re.M), name
    assert re.search(r"^disable-model-invocation: true$", fm, re.M), name  # stays out of the model's context
    assert re.search(r"^description: .+", fm, re.M), name
    cmd = re.search(r"```!\n(.*?)\n```", text, re.S).group(1)
    assert cmd.startswith('"${CLAUDE_SKILL_DIR}/../../bin/gutcheck" ' + sub), cmd
    assert os.access(os.path.join(ROOT, "skills", name, "..", "..", "bin", "gutcheck"), os.X_OK)
recall = open(os.path.join(ROOT, "skills", "recall", "SKILL.md")).read()
assert "<<'GUTCHECK_QUESTION'\n$ARGUMENTS\nGUTCHECK_QUESTION" in recall  # quotes and $ in the question stay literal
print("owner commands ok")
