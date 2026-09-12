// Package ids mints the identifiers that tie our records to broker orders.
package ids

import (
	"crypto/rand"
	"encoding/hex"
)

// New returns a random RFC 4122 version 4 UUID.
//
// Every order carries one as its client order id, so it must be unique and
// unguessable: a collision would let one broker order answer for another.
func New() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand does not fail on the platforms we run on, and a silent
		// fallback would weaken every identifier that follows.
		panic("난수 생성 실패: " + err.Error())
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant

	out := make([]byte, 36)
	hex.Encode(out[0:8], b[0:4])
	out[8] = '-'
	hex.Encode(out[9:13], b[4:6])
	out[13] = '-'
	hex.Encode(out[14:18], b[6:8])
	out[18] = '-'
	hex.Encode(out[19:23], b[8:10])
	out[23] = '-'
	hex.Encode(out[24:36], b[10:16])
	return string(out)
}
