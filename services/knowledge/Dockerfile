FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -o /out/knowledge-service ./cmd/server

FROM alpine:3.20
COPY --from=build /out/knowledge-service /knowledge-service
EXPOSE 8090
ENTRYPOINT ["/knowledge-service"]
