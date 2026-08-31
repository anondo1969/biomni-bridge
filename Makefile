.PHONY: help install install-release check test lint fmt smoke package release-check endpoint-check endpoint-diagnose build build-native build-amd64 build-arm64 build-multi build-multi-push run clean

IMAGE ?= biomni-bridge:local
MULTI_IMAGE ?=
PLATFORMS ?= linux/amd64,linux/arm64

help:
	@echo "install           install the wrapper with development tools"
	@echo "install-release   install development + package release tools"
	@echo "check             run lint, tests, and runtime smoke checks"
	@echo "package           build wheel/sdist and validate PyPI metadata"
	@echo "release-check     run check + package before tagging a release"
	@echo "test              run unit tests"
	@echo "lint              run Ruff"
	@echo "fmt               format source/tests and apply safe Ruff fixes"
	@echo "smoke             verify Biomni 0.0.8, scientific stack, and PDF runtime"
	@echo "endpoint-check    test /models and one minimal chat request against the configured API"
	@echo "endpoint-diagnose test output caps, Qwen thinking control, and non-streaming throughput"
	@echo "build             build a native local Docker image (same as build-native)"
	@echo "build-native      build for the current machine architecture"
	@echo "build-amd64       build/load a linux/amd64 local image"
	@echo "build-arm64       build/load a linux/arm64 local image"
	@echo "build-multi       verify both amd64 + arm64 builds without publishing"
	@echo "build-multi-push  build/push one multi-platform manifest (set MULTI_IMAGE=...)"
	@echo "run               start the app with docker compose"
	@echo "clean             remove local Python build/test caches"

install:
	python -m pip install -e '.[dev]'

install-release:
	python -m pip install -e '.[dev,release]'

check: lint test smoke

package:
	rm -rf dist build
	python -m build
	python -m twine check dist/*

release-check: check package

test:
	python -m pytest -q

lint:
	python -m ruff check src tests scripts

fmt:
	python -m ruff format src tests scripts
	python -m ruff check --fix src tests scripts

smoke:
	python scripts/smoke_import.py

endpoint-check:
	biomni-bridge-endpoint-check

endpoint-diagnose:
	biomni-bridge-endpoint-diagnose

build: build-native

build-native:
	docker build -t $(IMAGE) .

build-amd64:
	docker buildx build --platform linux/amd64 --load -t $(IMAGE)-amd64 .

build-arm64:
	docker buildx build --platform linux/arm64 --load -t $(IMAGE)-arm64 .

# A multi-platform result cannot be loaded into the classic local Docker image
# store as one manifest. This target executes both builds and discards only the
# final image output; all Dockerfile RUN/smoke checks still execute.
build-multi:
	docker buildx build --platform $(PLATFORMS) --output=type=cacheonly .

# Example:
# make build-multi-push MULTI_IMAGE=ghcr.io/owner/biomni-bridge:test
build-multi-push:
	@test -n "$(MULTI_IMAGE)" || (echo "Set MULTI_IMAGE=registry/name:tag" >&2; exit 2)
	docker buildx build --platform $(PLATFORMS) --push -t $(MULTI_IMAGE) .

run:
	docker compose up

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
