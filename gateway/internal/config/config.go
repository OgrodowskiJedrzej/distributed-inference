package config

import (
	"fmt"
	"os"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

type ServerConfig struct {
	Host string `yaml:"host"`
	Port string `yaml:"port"`
}

type RedisConfig struct {
	Address  string `yaml:"address"`
	Password string `yaml:"password"`
	DB       int    `yaml:"db"`
}

type RateLimitConfig struct {
	RequestsPerMinute int64         `yaml:"requests_per_minute"`
	TokensPerMinute   int64         `yaml:"tokens_per_minute"`
	MaxConcurrency    int64         `yaml:"max_concurrency"`
	DefaultMaxTokens  int64         `yaml:"default_max_tokens"`
	ReservationTTL    time.Duration `yaml:"reservation_ttl"`
}

type ModelConfig struct {
	MaxTokens int64 `yaml:"max_tokens"`
}

type Config struct {
	Server    ServerConfig    `yaml:"server"`
	Redis     RedisConfig     `yaml:"redis"`
	RateLimit RateLimitConfig `yaml:"rate_limit"`
	Models    map[string]ModelConfig `yaml:"models"`
	Upstream  string          `yaml:"upstream"`
}

func Load() (Config, error) {
	data, err := os.ReadFile("config.yaml")
	if err != nil {
		return Config{}, err
	}

	var config Config
	if err := yaml.Unmarshal(data, &config); err != nil {
		return Config{}, err
	}

	if upstream := os.Getenv("UPSTREAM_URL"); upstream != "" {
		config.Upstream = upstream
	}

	if address := os.Getenv("REDIS_ADDRESS"); address != "" {
		config.Redis.Address = address
	}

	if password := os.Getenv("REDIS_PASSWORD"); password != "" {
		config.Redis.Password = password
	}

	if len(config.Models) == 0 {
		return Config{}, fmt.Errorf("models must not be empty")
	}
	for name, model := range config.Models {
		if strings.TrimSpace(name) == "" {
			return Config{}, fmt.Errorf("model name must not be empty")
		}
		if model.MaxTokens <= 0 {
			return Config{}, fmt.Errorf("model %q max_tokens must be positive", name)
		}
	}

	return config, nil
}
