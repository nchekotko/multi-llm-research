# Build this image explicitly. Runtime jobs cannot install dependencies or pull images.
FROM python:3.12.12-slim
COPY runner.py /runner.py
USER 65532:65532
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "-B", "/runner.py"]
