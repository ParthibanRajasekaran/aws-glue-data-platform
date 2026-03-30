.PHONY: lint test test-e2e deploy verify install

install:
	pip install pre-commit && pre-commit install

lint:
	pre-commit run --all-files

test:
	pytest tests/unit/ -v

test-e2e:
	pytest tests/e2e/ -v -m e2e

deploy:
	bash deploy.sh

verify:
	bash verify_api.sh
