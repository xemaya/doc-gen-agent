FROM 050027656530.dkr.ecr.us-east-1.amazonaws.com/a2h/agent-base:python-3.12-http

# WeasyPrint needs native pango/cairo/harfbuzz; CJK fonts prevent tofu
# squares on Chinese text. Install as root then drop back to the
# non-root agent user that the base image configured.
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libfontconfig1 \
      libcairo2 libgdk-pixbuf-2.0-0 shared-mime-info \
      fonts-noto-cjk fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*
USER agent

COPY --chown=agent:agent . /opt/agent
WORKDIR /opt/agent

RUN pip install --no-cache-dir -r requirements.txt

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
