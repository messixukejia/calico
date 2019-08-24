package intdataplane

import (
	"net"
	"time"

	"github.com/projectcalico/felix/rules"

	"github.com/projectcalico/felix/ip"

	"github.com/projectcalico/felix/routetable"

	. "github.com/onsi/ginkgo"
	. "github.com/onsi/gomega"
	"github.com/projectcalico/felix/proto"
	"github.com/vishvananda/netlink"
)

type mockVXLANDataplane struct {
	links []netlink.Link
}

func (m *mockVXLANDataplane) LinkByName(name string) (netlink.Link, error) {
	return &netlink.Vxlan{
		LinkAttrs: netlink.LinkAttrs{
			Name: "vxlan",
		},
		VxlanId:      1,
		Port:         20,
		VtepDevIndex: 2,
		SrcAddr:      ip.FromString("172.0.0.2").AsNetIP()}, nil
}

func (m *mockVXLANDataplane) LinkSetMTU(link netlink.Link, mtu int) error {
	return nil
}

func (m *mockVXLANDataplane) LinkSetUp(link netlink.Link) error {
	return nil
}

func (m *mockVXLANDataplane) AddrList(link netlink.Link, family int) ([]netlink.Addr, error) {
	l := []netlink.Addr{{
		IPNet: &net.IPNet{
			IP: net.IPv4(172, 0, 0, 2),
		},
	},
	}
	return l, nil
}

func (m *mockVXLANDataplane) AddrAdd(link netlink.Link, addr *netlink.Addr) error {
	return nil
}

func (m *mockVXLANDataplane) AddrDel(link netlink.Link, addr *netlink.Addr) error {
	return nil
}

func (m *mockVXLANDataplane) LinkList() ([]netlink.Link, error) {
	return m.links, nil
}

func (m *mockVXLANDataplane) LinkAdd(netlink.Link) error {
	return nil
}
func (m *mockVXLANDataplane) LinkDel(netlink.Link) error {
	return nil
}

var _ = Describe("VXLANManager", func() {
	var manager *vxlanManager
	var rt *mockRouteTable
	var prt *mockRouteTable

	BeforeEach(func() {
		rt = &mockRouteTable{
			currentRoutes:   map[string][]routetable.Target{},
			currentL2Routes: map[string][]routetable.L2Target{},
		}
		prt = &mockRouteTable{
			currentRoutes:   map[string][]routetable.Target{},
			currentL2Routes: map[string][]routetable.L2Target{},
		}

		manager = newVXLANManagerWithShims(
			newMockIPSets(),
			rt,
			"vxlan.calico",
			Config{
				MaxIPSetSize:       5,
				Hostname:           "node1",
				ExternalNodesCidrs: []string{"10.0.0.0/24"},
				RulesConfig: rules.Config{
					VXLANVNI:  1,
					VXLANPort: 20,
				},
			},
			&mockVXLANDataplane{
				links: []netlink.Link{&mockLink{attrs: netlink.LinkAttrs{Name: "eth0"}}},
			},
			func(interfacePrefixes []string, ipVersion uint8, vxlan bool, netlinkTimeout time.Duration,
				deviceRouteSourceAddress net.IP, deviceRouteProtocol int, removeExternalRoutes bool) routeTable {
				return prt
			},
		)
	})

	It("successfully adds a route to the parent interface", func() {
		manager.OnUpdate(&proto.VXLANTunnelEndpointUpdate{
			Node:           "node1",
			Mac:            "00:0a:74:9d:68:16",
			Ipv4Addr:       "10.0.0.0",
			ParentDeviceIp: "172.0.0.2",
		})

		manager.OnUpdate(&proto.VXLANTunnelEndpointUpdate{
			Node:           "node2",
			Mac:            "00:0a:95:9d:68:16",
			Ipv4Addr:       "10.0.80.0/32",
			ParentDeviceIp: "172.0.12.1",
		})

		localVTEP := manager.getLocalVTEP()
		Expect(localVTEP).NotTo(BeNil())

		manager.noEncapRouteTable = prt

		err := manager.configureVXLANDevice(50, localVTEP)
		Expect(err).NotTo(HaveOccurred())

		Expect(manager.myVTEP).NotTo(BeNil())
		Expect(manager.noEncapRouteTable).NotTo(BeNil())
		parent, err := manager.getLocalVTEPParent()

		Expect(parent).NotTo(BeNil())
		Expect(err).NotTo(HaveOccurred())

		manager.OnUpdate(&proto.RouteUpdate{
			Type: proto.RouteType_NOENCAP,
			Node: "node2",
			Dst:  "172.0.0.1/26",
			Gw:   "172.8.8.8/32",
		})

		manager.OnUpdate(&proto.RouteUpdate{
			Type: proto.RouteType_VXLAN,
			Node: "node2",
			Dst:  "172.0.0.2/26",
			Gw:   "172.8.8.8/32",
		})

		Expect(rt.currentRoutes["vxlan.calico"]).To(HaveLen(0))

		err = manager.CompleteDeferredWork()

		Expect(err).NotTo(HaveOccurred())
		Expect(rt.currentRoutes["vxlan.calico"]).To(HaveLen(1))
		Expect(prt.currentRoutes["eth0"]).NotTo(BeNil())
	})

	It("adds the route to the default table on next try when the parent route table is not immediately found", func() {
		go manager.KeepVXLANDeviceInSync(1400, 1*time.Second)
		manager.OnUpdate(&proto.VXLANTunnelEndpointUpdate{
			Node:           "node2",
			Mac:            "00:0a:95:9d:68:16",
			Ipv4Addr:       "10.0.80.0/32",
			ParentDeviceIp: "172.0.12.1",
		})

		manager.OnUpdate(&proto.RouteUpdate{
			Type: proto.RouteType_NOENCAP,
			Node: "node2",
			Dst:  "172.0.0.1/32",
			Gw:   "172.8.8.8/32",
		})

		err := manager.CompleteDeferredWork()

		Expect(err).NotTo(BeNil())
		Expect(err.Error()).To(Equal("no encap route table not set, will defer adding routes"))
		Expect(manager.routesDirty).To(BeTrue())

		manager.OnUpdate(&proto.VXLANTunnelEndpointUpdate{
			Node:           "node1",
			Mac:            "00:0a:74:9d:68:16",
			Ipv4Addr:       "10.0.0.0",
			ParentDeviceIp: "172.0.0.2",
		})

		time.Sleep(2 * time.Second)

		localVTEP := manager.getLocalVTEP()
		Expect(localVTEP).NotTo(BeNil())

		err = manager.configureVXLANDevice(50, localVTEP)
		Expect(err).NotTo(HaveOccurred())

		Expect(prt.currentRoutes["eth0"]).To(HaveLen(0))
		err = manager.CompleteDeferredWork()

		Expect(err).NotTo(HaveOccurred())
		Expect(manager.routesDirty).To(BeFalse())
		Expect(prt.currentRoutes["eth0"]).To(HaveLen(1))
	})
})
