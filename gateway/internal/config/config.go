package config

import (
	"os"
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

type Config struct {
	Server    ServerConfig    `yaml:"server"`
	Redis     RedisConfig     `yaml:"redis"`
	RateLimit RateLimitConfig `yaml:"rate_limit"`
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

	return config, nil
}
