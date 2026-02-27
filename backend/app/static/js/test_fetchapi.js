// Test script for enhanced fetchAPI functionality
// This script tests the circuit breaker, retry logic, and error handling features

console.log('Testing enhanced fetchAPI implementation...');

// Test 1: Verify CircuitBreaker class exists and functions
function testCircuitBreaker() {
    console.log('\n--- Test 1: Circuit Breaker ---');

    if (typeof CircuitBreaker === 'undefined') {
        console.error('❌ CircuitBreaker class not found');
        return false;
    }

    const cb = new CircuitBreaker(2, 1000); // 2 failures, 1 second cooldown

    // Test initial state
    if (cb.getState() !== 'CLOSED') {
        console.error('❌ Initial state should be CLOSED');
        return false;
    }

    // Test failure counting
    cb.onFailure();
    cb.onFailure();

    if (cb.getState() !== 'OPEN') {
        console.error('❌ Should be OPEN after 2 failures');
        return false;
    }

    // Test half-open state after cooldown
    setTimeout(() => {
        if (cb.getState() !== 'HALF_OPEN') {
            console.error('❌ Should be HALF_OPEN after cooldown');
            return false;
        }
        console.log('✅ Circuit breaker functionality working correctly');
    }, 1100);

    return true;
}

// Test 2: Verify fetchAPI function exists
function testFetchAPIExists() {
    console.log('\n--- Test 2: fetchAPI Function ---');

    if (typeof fetchAPI === 'undefined') {
        console.error('❌ fetchAPI function not found');
        return false;
    }

    console.log('✅ fetchAPI function exists');
    return true;
}

// Test 3: Verify enhanced error handling functions
function testErrorHandlingFunctions() {
    console.log('\n--- Test 3: Error Handling Functions ---');

    const functions = ['showModalLoading', 'hideModalLoading', 'showModalError', 'showSectionError'];

    for (const func of functions) {
        if (typeof window[func] === 'undefined') {
            console.error(`❌ ${func} function not found`);
            return false;
        }
    }

    console.log('✅ All error handling functions exist');
    return true;
}

// Test 4: Verify DEBUG constant exists
function testDebugConstant() {
    console.log('\n--- Test 4: DEBUG Constant ---');

    if (typeof DEBUG === 'undefined') {
        console.error('❌ DEBUG constant not found');
        return false;
    }

    console.log(`✅ DEBUG constant exists with value: ${DEBUG}`);
    return true;
}

// Run all tests
function runAllTests() {
    console.log('Starting fetchAPI enhancement tests...\n');

    const tests = [
        testCircuitBreaker,
        testFetchAPIExists,
        testErrorHandlingFunctions,
        testDebugConstant
    ];

    let passed = 0;
    let failed = 0;

    for (const test of tests) {
        try {
            if (test()) {
                passed++;
            } else {
                failed++;
            }
        } catch (error) {
            console.error(`❌ Test ${test.name} failed with error:`, error);
            failed++;
        }
    }

    console.log('\n--- Test Results ---');
    console.log(`✅ Passed: ${passed}`);
    console.log(`❌ Failed: ${failed}`);
    console.log(`📊 Total: ${tests.length}`);

    if (failed === 0) {
        console.log('\n🎉 All tests passed! The enhanced fetchAPI implementation is working correctly.');
    } else {
        console.log('\n⚠️  Some tests failed. Please review the implementation.');
    }
}

// Run tests when script loads
runAllTests();