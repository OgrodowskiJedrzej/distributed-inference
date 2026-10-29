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
	Backends []string     `yaml:"backends"`
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

	return config, nil
}
