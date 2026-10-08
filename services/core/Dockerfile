FROM golang:1.27.0-alpine AS build
ARG GOPROXY=https://goproxy.cn,direct
ENV GOPROXY=${GOPROXY}
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -o /out/core-service ./cmd/server

FROM alpine:3.20
COPY --from=build /out/core-service /core-service
EXPOSE 8080
ENTRYPOINT ["/core-service"]
