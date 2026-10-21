ARG LITELLM_TAG=v1.106.1
FROM docker.litellm.ai/berriai/litellm:${LITELLM_TAG}
COPY dist/ /tmp/brownout-dist/
RUN python -m ensurepip --upgrade && python -m pip install --no-deps /tmp/brownout-dist/litellm_brownout-*.whl
