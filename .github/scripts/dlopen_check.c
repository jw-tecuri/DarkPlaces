/* Load the same dylibs DarkPlaces dlopens and resolve one symbol from each.
 * Arguments are pairs: <absolute-or-relative-path> <symbol>
 */
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
	int fail = 0;
	int i;

	if (argc < 3 || ((argc - 1) % 2) != 0) {
		fprintf(stderr, "usage: %s path symbol [path symbol ...]\n", argv[0]);
		return 2;
	}

	for (i = 1; i < argc; i += 2) {
		const char *path = argv[i];
		const char *symbol = argv[i + 1];
		void *handle;
		void *sym;

		handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
		if (!handle) {
			fprintf(stderr, "FAIL dlopen %s: %s\n", path, dlerror());
			fail = 1;
			continue;
		}
		(void)dlerror();
		sym = dlsym(handle, symbol);
		if (!sym) {
			fprintf(stderr, "FAIL dlsym %s in %s: %s\n", symbol, path, dlerror());
			fail = 1;
		} else {
			printf("OK %s (%s)\n", path, symbol);
		}
		dlclose(handle);
	}

	return fail;
}
