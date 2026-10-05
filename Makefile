SHELL := /bin/bash

init:
	python setup.py develop
	pip install -r requirements.txt

test:
	rm -f .coverage
	pytest

# Local, deterministic subset for the frozen-clock subscription interface.
# Disables plugin autoload to avoid unrelated broken third-party pytest plugins.
pytest-local:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_subscriptions.py -q

tag:
	python create_tag.py

publish:
	rm -rf dist
	python -m build
	twine upload dist/*

venv:
	virtualenv venv
	venv/bin/pip install -r requirements.txt
