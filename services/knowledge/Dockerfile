FROM golang:1.27.0-alpine AS build
ARG GOPROXY=https://goproxy.cn,direct
ENV GOPROXY=${GOPROXY}
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -o /out/knowledge-service ./cmd/server

FROM alpine:3.20
COPY --from=build /out/knowledge-service /knowledge-service
EXPOSE 8090
ENTRYPOINT ["/knowledge-service"]
