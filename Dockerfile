# The build stage always runs on the builder's own architecture and Go cross
# compiles, so a multi-arch image needs no emulation.
FROM --platform=$BUILDPLATFORM golang:1.26-bookworm AS build
ARG TARGETARCH
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd/ cmd/
COPY internal/ internal/
RUN CGO_ENABLED=0 GOOS=linux GOARCH=$TARGETARCH \
    go build -trimpath -ldflags="-s -w" -o /out/cotrader ./cmd/cotrader

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/cotrader /cotrader
USER nonroot:nonroot
ENTRYPOINT ["/cotrader"]
