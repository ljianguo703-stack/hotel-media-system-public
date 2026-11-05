import java.lang.reflect.Method;

/** Exact-firmware maintenance helper. Uses the TV's own factory service, without root. */
public final class PowerModeTool {
    private static String property(String key) throws Exception {
        Class<?> props = Class.forName("android.os.SystemProperties");
        return (String) props.getMethod("get", String.class).invoke(null, key);
    }

    public static void main(String[] args) {
        Thread timer = new Thread(() -> {
            try {
                Thread.sleep(15000);
                System.exit(3);
            } catch (InterruptedException ignored) {
            }
        });
        timer.setDaemon(true);
        timer.start();
        int exit = 1;
        try {
            if (args.length != 1 || !(args[0].equals("read") || args[0].equals("enable")
                    || args[0].equals("restore-secondary"))) {
                throw new IllegalArgumentException("Expected read, enable or restore-secondary");
            }
            if (!property("ro.product.model").equals("MiTV-ASTP0")
                    || !property("ro.product.device").equals("mulan")
                    || !property("ro.build.fingerprint").equals(
                    "Xiaomi/mulan/mulan:9/PPR1.180610.011/OS3.0.104.0.PSTAATV:user/release-keys")) {
                throw new IllegalStateException("Model/firmware does not match the pilot profile");
            }
            Class<?> api = Class.forName("xiaomi.hardware.factoryserver.V1_0.IFactory");
            Object service = api.getMethod("getService", String.class, boolean.class)
                    .invoke(null, "default", false);
            if (service == null) {
                throw new IllegalStateException("Native factory service unavailable");
            }
            Method getter = api.getMethod("Get_factory_data", String.class);
            String before = (String) getter.invoke(service, "factory_power_mode");
            if (!("secondary".equals(before) || "direct".equals(before))) {
                throw new IllegalStateException("Unknown original power mode");
            }
            System.out.println("MODE_BEFORE=" + before);
            if (!args[0].equals("read")) {
                String target = args[0].equals("enable") ? "direct" : "secondary";
                String after = before;
                if (!before.equals(target)) {
                    int result = (Integer) api.getMethod("Set_factory_data", String.class, String.class)
                            .invoke(service, "factory_power_mode", target);
                    System.out.println("NATIVE_RESULT=" + result);
                    after = (String) getter.invoke(service, "factory_power_mode");
                }
                System.out.println("MODE_AFTER=" + after);
                // This firmware returns 1 on a successful write. Confirm actual storage instead.
                if (!target.equals(after)) {
                    throw new IllegalStateException("Power mode write was not verified");
                }
            }
            exit = 0;
        } catch (Throwable error) {
            System.out.println("ERROR=" + error);
            if (error.getCause() != null) {
                System.out.println("CAUSE=" + error.getCause());
            }
        }
        System.exit(exit);
    }
}
