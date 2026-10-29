package backend

type Pool struct {
	addresses []string
}

func NewPool(addresses []string) *Pool {
	return &Pool{addresses: addresses}
}

func (p *Pool) Len() int {
	return len(p.addresses)
}

func (p *Pool) Address(index int) string {
	return p.addresses[index]
}
