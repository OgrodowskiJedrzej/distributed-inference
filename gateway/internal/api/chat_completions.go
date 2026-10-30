package api

import (
	"net/http"
	"net/http/httputil"
	"net/url"
)

func NewChatCompletionsHandler(upstream *url.URL) http.Handler {
	return &httputil.ReverseProxy{
		Rewrite: func(request *httputil.ProxyRequest) {
			request.SetURL(upstream)
		},
	}
}
