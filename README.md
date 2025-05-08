# works on format1 only

## tables issues pending

## run endpoint_upload in a separate terminal

### dont forget playwright install

### then install bowser_use

### screen S extractor

### screen S guru

docker pull teledoc2/document-extractor:0.1

docker run -d --name doc-extractor \
  --restart unless-stopped \
  -p 127.0.0.1:8007:8007 \
  -v /srv/document-extractor/uploads:/app/uploads \
  -v /srv/document-extractor/archives:/app/archives \
  -v /srv/envs/doc-extractor.env:/app/.env:ro \
  yourhub/doc-extractor:0.1

Docker compose


