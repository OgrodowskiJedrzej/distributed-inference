package config

import (
	"os"

	"gopkg.in/yaml.v3"
)

type ServerConfig struct {
	Host string `yaml:"host"`
	Port string `yaml:"port"`
}

type Config struct {
	Server   ServerConfig `yaml:"server"`
	Upstream string       `yaml:"upstream"`
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

	return config, nil
}
