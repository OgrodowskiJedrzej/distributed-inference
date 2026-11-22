FROM golang:1.27-alpine AS build

WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 go build -o /gateway ./cmd

FROM alpine:3.22

WORKDIR /app

COPY --from=build /gateway /app/gateway

USER nobody
EXPOSE 8000

ENTRYPOINT ["/app/gateway"]
