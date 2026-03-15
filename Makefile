.PHONY: lint test install

lint:
	ruff format .
	ruff check . --fix

test:
	python3 -m pytest tests/test_compact_check.py -v
	node --test tests/test_context_monitor.js

install:
	bash install.sh
