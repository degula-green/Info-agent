FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY . .
RUN go build -o /out/core-service ./cmd/server

FROM alpine:3.20
COPY --from=build /out/core-service /core-service
EXPOSE 8080
ENTRYPOINT ["/core-service"]
