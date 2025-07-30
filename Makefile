.PHONY: install test lint

install:
	sudo ./install.sh

test:
	python -m venv venv && . venv/bin/activate && \
	pip install -r requirements.txt pytest && \
	pytest -q tests

lint:
	flake8 apply_patch.py review.py
