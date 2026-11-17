"""Skills (a plugin folder in git, starting with the starter skills) and projects (folders)."""

import json
import subprocess

from alfred.projects import GENERAL, Projects
from alfred.skills import Skills, frontmatter


def commits(root):
    out = subprocess.run(["git", "log", "--oneline"], cwd=root, capture_output=True, text=True)
    return out.stdout.strip().splitlines()


STARTERS = ["answering-from-documents", "writing-skills"]


def test_skills_start_with_the_starter_skills(tmp_path):
    skills = Skills(tmp_path / "skills")
    manifest = json.loads((skills.root / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "alfred"
    assert [s["name"] for s in skills.list()] == STARTERS
    assert "teaches you" in skills.list()[1]["use_when"]
    assert len(commits(skills.root)) == 1
    Skills(skills.root)  # opening it again changes nothing
    assert len(commits(skills.root)) == 1


def test_a_new_starter_skill_reaches_an_existing_install_once(tmp_path):
    skills = Skills(tmp_path / "skills")
    (skills.root / ".claude-plugin" / "starters.json").write_text('["writing-skills"]')
    assert skills.remove("answering-from-documents")  # as if it came in a later version
    Skills(skills.root)
    assert [s["name"] for s in skills.list()] == STARTERS  # added
    assert skills.remove("answering-from-documents")
    Skills(skills.root)
    assert [s["name"] for s in skills.list()] == ["writing-skills"]  # forgotten stays so


def test_new_and_forgotten_skills_are_commits(tmp_path):
    skills = Skills(tmp_path / "skills")
    deploy = skills.folder / "deploy-portfolio"
    deploy.mkdir()
    (deploy / "SKILL.md").write_text("---\nname: deploy-portfolio\ndescription: Deploy it.\n---\n")
    assert skills.commit("Learned deploy-portfolio")
    assert not skills.commit("Nothing changed")
    assert {s["name"] for s in skills.list()} == {*STARTERS, "deploy-portfolio"}
    assert skills.remove("deploy-portfolio") and not skills.remove("deploy-portfolio")
    assert not skills.remove("../..")  # only a skill's own folder
    assert len(commits(skills.root)) == 3


def test_frontmatter():
    text = '---\nname: a-skill\ndescription: "Use it: when asked."\n---\n# Steps'
    assert frontmatter(text) == {"name": "a-skill", "description": "Use it: when asked."}
    assert frontmatter("no frontmatter") == {}


def test_projects_are_found_by_their_spoken_names(tmp_path):
    projects = Projects(tmp_path / "projects")
    projects.path("portfolio-site")
    assert projects.resolve("the Portfolio Site project") == "portfolio-site"
    assert projects.resolve("Budget App") == "budget-app"  # a new one
    assert projects.resolve("") == GENERAL and projects.resolve("the project") == GENERAL
    assert projects.names() == ["portfolio-site"]


def test_project_instructions_reach_the_agent(tmp_path):
    projects = Projects(tmp_path / "projects")
    assert projects.instructions("app") == ""
    (projects.path("app") / "AGENTS.md").write_text("Use pnpm.")
    assert "Use pnpm." in projects.instructions("app")
